"""Runtime-maintained market resolution feed.

The feed is a read-only data source for replay/scoring. It refreshes finalized
market outcomes into derived JSONL artifacts and never mutates paper/live
wallets, decision ledgers, or accounting state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from typing import Any, Mapping

from bot.file_ops import atomic_write_json
from bot.scoreboard_resolution_backfill import MarketFetcher, backfill_scoreboard_resolutions

DEFAULT_OUTPUT_DIR = "data/beta_shadow/resolution_feed"
DEFAULT_INTERVAL_SECONDS = 1800


@dataclass(frozen=True, slots=True)
class ResolutionFeedResult:
    status: str
    refreshed: bool
    reason: str | None
    output_path: Path | None
    report_path: Path | None
    state_path: Path
    market_ref_path: Path | None = None
    resolved_market_count: int = 0
    unresolved_market_count: int = 0
    fetch_error_count: int = 0


def run_resolution_feed_once(
    config: Mapping[str, Any],
    *,
    now: datetime | None = None,
    fetch_market: MarketFetcher | None = None,
    force: bool = False,
) -> ResolutionFeedResult:
    feed_cfg = normalize_resolution_feed_config(config)
    output_dir = Path(str(feed_cfg["output_dir"]))
    central_output_dir = Path(str(feed_cfg["central_output_dir"]))
    state_path = output_dir / "state.json"
    if not feed_cfg["enabled"]:
        return ResolutionFeedResult(
            status="disabled",
            refreshed=False,
            reason="disabled",
            output_path=None,
            report_path=None,
            state_path=state_path,
        )

    now_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
    state = _load_state(state_path)
    if not force and not _due(state, now=now_dt, interval_seconds=int(feed_cfg["interval_seconds"])):
        return ResolutionFeedResult(
            status="skipped",
            refreshed=False,
            reason="not_due",
            output_path=_optional_path(state.get("latest_resolution_path")),
            report_path=_optional_path(state.get("latest_report_path")),
            state_path=state_path,
            market_ref_path=_optional_path(state.get("market_ref_path")),
            resolved_market_count=int(state.get("resolved_market_count") or 0),
            unresolved_market_count=int(state.get("unresolved_market_count") or 0),
            fetch_error_count=int(state.get("fetch_error_count") or 0),
        )

    decision_ledger_paths = [Path(str(path)) for path in feed_cfg["decision_ledger_paths"]]
    existing_decision_ledger_paths = [path for path in decision_ledger_paths if path.exists()]
    missing_decision_ledger_paths = [path for path in decision_ledger_paths if not path.exists()]
    if not existing_decision_ledger_paths:
        _write_state(
            state_path,
            {
                "schema_name": "resolution_feed_state",
                "schema_version": 1,
                "status": "missing_input",
                "last_refresh_at": _iso(now_dt),
                "decision_ledger_path": str(decision_ledger_paths[0]) if decision_ledger_paths else "",
                "decision_ledger_paths": [],
                "configured_decision_ledger_paths": [str(path) for path in decision_ledger_paths],
                "used_decision_ledger_paths": [],
                "missing_decision_ledger_paths": [str(path) for path in missing_decision_ledger_paths],
                "decision_ledger_globs": list(feed_cfg["decision_ledger_globs"]),
            },
        )
        return ResolutionFeedResult(
            status="missing_input",
            refreshed=False,
            reason="missing_decision_ledger",
            output_path=None,
            report_path=None,
            state_path=state_path,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    central_output_dir.mkdir(parents=True, exist_ok=True)
    lane_latest_resolution = output_dir / "latest_resolutions.jsonl"
    central_latest_resolution = central_output_dir / "latest_resolutions.jsonl"
    _seed_central_resolutions_from_lane_latest(central_latest_resolution, lane_latest_resolution)
    market_ref_path = _market_ref_path_for_refresh(
        existing_decision_ledger_paths,
        output_dir=output_dir,
        existing_latest_path=central_latest_resolution,
        mode=str(feed_cfg["mode"]),
        max_incremental_markets=feed_cfg["max_incremental_markets"],
    )
    stamp = now_dt.strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"resolutions_{stamp}.jsonl"
    report_path = output_dir / f"resolutions_{stamp}.report.json"
    result = backfill_scoreboard_resolutions(
        [market_ref_path],
        output_path=output_path,
        report_path=report_path,
        fetch_market=fetch_market,
        include_unresolved=False,
        max_markets=feed_cfg["max_markets"],
        max_fetch_attempts=int(feed_cfg["max_fetch_attempts"]),
        retry_delay_seconds=float(feed_cfg["retry_delay_seconds"]),
    )
    latest_report = output_dir / "latest_resolutions.report.json"
    if feed_cfg["mode"] == "incremental_unresolved":
        _write_merged_latest_resolutions(central_latest_resolution, result.resolution_rows)
    else:
        central_latest_resolution.write_text(output_path.read_text(encoding="utf-8"), encoding="utf-8")
    if lane_latest_resolution != central_latest_resolution:
        lane_latest_resolution.write_text(central_latest_resolution.read_text(encoding="utf-8"), encoding="utf-8")
    latest_report.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
    latest_rows = _load_resolution_rows(central_latest_resolution)
    state_payload = {
        "schema_name": "resolution_feed_state",
        "schema_version": 1,
        "status": "refreshed",
        "mode": feed_cfg["mode"],
        "last_refresh_at": _iso(now_dt),
        "decision_ledger_path": str(existing_decision_ledger_paths[0]) if existing_decision_ledger_paths else "",
        "decision_ledger_paths": [str(path) for path in existing_decision_ledger_paths],
        "configured_decision_ledger_paths": [str(path) for path in decision_ledger_paths],
        "used_decision_ledger_paths": [str(path) for path in existing_decision_ledger_paths],
        "missing_decision_ledger_paths": [str(path) for path in missing_decision_ledger_paths],
        "decision_ledger_globs": list(feed_cfg["decision_ledger_globs"]),
        "market_ref_path": str(market_ref_path),
        "latest_resolution_path": str(central_latest_resolution),
        "central_resolution_path": str(central_latest_resolution),
        "compatibility_latest_resolution_path": str(lane_latest_resolution),
        "latest_report_path": str(latest_report),
        "resolved_market_count": len(latest_rows) if feed_cfg["mode"] == "incremental_unresolved" else result.report.get("resolved_market_count", 0),
        "unresolved_market_count": result.report.get("unresolved_market_count", 0),
        "fetch_error_count": result.report.get("fetch_error_count", 0),
        "retryable_fetch_error_count": result.report.get("retryable_fetch_error_count", 0),
    }
    _write_state(state_path, state_payload)
    return ResolutionFeedResult(
        status="refreshed",
        refreshed=True,
        reason=None,
        output_path=central_latest_resolution,
        report_path=latest_report,
        state_path=state_path,
        market_ref_path=market_ref_path,
        resolved_market_count=int(state_payload["resolved_market_count"] or 0),
        unresolved_market_count=int(result.report.get("unresolved_market_count") or 0),
        fetch_error_count=int(result.report.get("fetch_error_count") or 0),
    )


def normalize_resolution_feed_config(config: Mapping[str, Any]) -> dict[str, Any]:
    raw = config.get("resolution_feed") if isinstance(config.get("resolution_feed"), Mapping) else {}
    lab = config.get("prediction_lab") if isinstance(config.get("prediction_lab"), Mapping) else {}
    if isinstance(lab.get("resolution_feed"), Mapping):
        raw = {**raw, **dict(lab["resolution_feed"])}
    shadow = config.get("paper_shadow_lanes") if isinstance(config.get("paper_shadow_lanes"), Mapping) else {}
    decision_ledger_paths = _coerce_decision_ledger_paths(raw, shadow)
    output_dir = raw.get("output_dir") or DEFAULT_OUTPUT_DIR
    central_output_dir = raw.get("central_output_dir") or raw.get("canonical_output_dir") or output_dir
    return {
        "enabled": bool(raw.get("enabled", False)),
        "decision_ledger_path": str(decision_ledger_paths[0]) if decision_ledger_paths else "",
        "decision_ledger_paths": [str(path) for path in decision_ledger_paths],
        "decision_ledger_globs": list(_coerce_decision_ledger_globs(raw)),
        "output_dir": str(output_dir),
        "central_output_dir": str(central_output_dir),
        "mode": _resolution_feed_mode(raw.get("mode")),
        "interval_seconds": max(1, int(raw.get("interval_seconds", lab.get("resolve_interval_seconds", DEFAULT_INTERVAL_SECONDS)) or DEFAULT_INTERVAL_SECONDS)),
        "max_fetch_attempts": max(1, int(raw.get("max_fetch_attempts", 4) or 4)),
        "retry_delay_seconds": max(0.0, float(raw.get("retry_delay_seconds", 3.0) or 0.0)),
        "max_markets": _optional_int(raw.get("max_markets")),
        "max_incremental_markets": _optional_int(raw.get("max_incremental_markets")),
    }


def _coerce_decision_ledger_paths(raw: Mapping[str, Any], shadow: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("decision_ledger_paths", "ledger_paths"):
        configured = raw.get(key)
        if isinstance(configured, (list, tuple)):
            values.extend(configured)
        elif configured not in (None, ""):
            values.append(configured)
    for key in ("decision_ledger_path", "ledger_path"):
        configured = raw.get(key)
        if configured not in (None, ""):
            values.append(configured)
    if not values:
        for key in ("decision_ledger_path", "ledger_path"):
            configured = shadow.get(key)
            if configured not in (None, ""):
                values.append(configured)
    values.extend(_expand_decision_ledger_globs(_coerce_decision_ledger_globs(raw)))

    paths: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        paths.append(text)
    return paths


def _coerce_decision_ledger_globs(raw: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("decision_ledger_globs", "decision_ledger_path_globs", "ledger_path_globs"):
        configured = raw.get(key)
        if isinstance(configured, (list, tuple)):
            values.extend(configured)
        elif configured not in (None, ""):
            values.append(configured)

    patterns: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        patterns.append(text)
    return patterns


def _expand_decision_ledger_globs(patterns: list[str]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in sorted(glob(pattern)):
            if match in seen:
                continue
            seen.add(match)
            paths.append(match)
    return paths


def write_unique_market_refs(input_path: Path | list[Path] | tuple[Path, ...], *, output_dir: Path) -> Path:
    market_ids: set[str] = set()
    input_paths = [input_path] if isinstance(input_path, Path) else list(input_path)
    for path in input_paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, Mapping):
                    continue
                market_id = row_market_id(row)
                if market_id:
                    market_ids.add(market_id)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "resolution_market_refs.jsonl"
    output_path.write_text(
        "".join(json.dumps({"market_id": market_id}, sort_keys=True) + "\n" for market_id in sorted(market_ids)),
        encoding="utf-8",
    )
    return output_path


def row_market_id(row: Mapping[str, Any]) -> str | None:
    for value in (
        row.get("market_id"),
        _mapping(row.get("shared_candidate")).get("market_id"),
        _mapping(_mapping(row.get("provenance")).get("shared_candidate")).get("market_id"),
        _mapping(_mapping(row.get("provenance")).get("future_pnl_inputs")).get("market_id"),
    ):
        if value not in (None, ""):
            return str(value)
    return None


def _market_ref_path_for_refresh(
    decision_ledger_paths: list[Path],
    *,
    output_dir: Path,
    existing_latest_path: Path,
    mode: str,
    max_incremental_markets: int | None,
) -> Path:
    if mode != "incremental_unresolved":
        return write_unique_market_refs(decision_ledger_paths, output_dir=output_dir)

    all_refs_path = write_unique_market_refs(decision_ledger_paths, output_dir=output_dir)
    resolved_market_ids = {
        market_id
        for row in _load_resolution_rows(existing_latest_path)
        if (market_id := _row_resolution_market_id(row)) not in (None, "")
    }
    pending_market_ids: list[str] = []
    with all_refs_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            market_id = row_market_id(row) if isinstance(row, Mapping) else None
            if market_id and market_id not in resolved_market_ids:
                pending_market_ids.append(market_id)

    if max_incremental_markets is not None:
        pending_market_ids = pending_market_ids[:max(0, int(max_incremental_markets))]

    pending_path = output_dir / "resolution_market_refs.pending.jsonl"
    pending_path.write_text(
        "".join(json.dumps({"market_id": market_id}, sort_keys=True) + "\n" for market_id in pending_market_ids),
        encoding="utf-8",
    )
    return pending_path


def _write_merged_latest_resolutions(latest_resolution: Path, new_rows: list[dict[str, Any]]) -> None:
    by_market_id: dict[str, dict[str, Any]] = {}
    for row in _load_resolution_rows(latest_resolution):
        market_id = _row_resolution_market_id(row)
        if market_id:
            by_market_id[market_id] = row
    for row in new_rows:
        market_id = _row_resolution_market_id(row)
        if market_id:
            by_market_id[market_id] = row
    latest_resolution.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for _, row in sorted(by_market_id.items())),
        encoding="utf-8",
    )


def _seed_central_resolutions_from_lane_latest(central_latest_resolution: Path, lane_latest_resolution: Path) -> None:
    if central_latest_resolution.exists() or central_latest_resolution == lane_latest_resolution:
        return
    if not lane_latest_resolution.exists():
        return
    central_latest_resolution.parent.mkdir(parents=True, exist_ok=True)
    central_latest_resolution.write_text(lane_latest_resolution.read_text(encoding="utf-8"), encoding="utf-8")


def _load_resolution_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _row_resolution_market_id(row: Mapping[str, Any]) -> str | None:
    for key in ("market_id", "ticker", "market_ticker", "market"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    resolution = row.get("resolution")
    if isinstance(resolution, Mapping):
        for key in ("market_id", "ticker", "market_ticker", "market"):
            value = resolution.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _resolution_feed_mode(value: Any) -> str:
    mode = str(value or "full").strip()
    if mode not in {"full", "incremental_unresolved"}:
        raise ValueError(f"unsupported resolution_feed.mode: {mode}")
    return mode


def _due(state: Mapping[str, Any], *, now: datetime, interval_seconds: int) -> bool:
    last = _coerce_datetime(state.get("last_refresh_at"))
    if last is None:
        return True
    return (now - last).total_seconds() >= interval_seconds


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, dict(payload))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return max(0, int(value))


def _optional_path(value: Any) -> Path | None:
    return Path(str(value)) if value not in (None, "") else None


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
