"""Runtime-maintained market resolution feed.

The feed is a read-only data source for replay/scoring. It refreshes finalized
market outcomes into derived JSONL artifacts and never mutates paper/live
wallets, decision ledgers, or accounting state.
"""

from __future__ import annotations

import json
import os
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from typing import Any, Mapping

from bot.file_ops import atomic_write_json, locked_file, rewrite_jsonl
from bot.scoreboard_resolution_backfill import MarketFetcher, backfill_scoreboard_resolutions

DEFAULT_OUTPUT_DIR = "data/beta_shadow/resolution_feed"
DEFAULT_INTERVAL_SECONDS = 1800
DEFAULT_MAX_DISCOVERY_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_INDEX_BOOTSTRAP_BYTES = 512 * 1024 * 1024


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

    market_ref_paths = [Path(str(path)) for path in feed_cfg["market_ref_paths"]]
    existing_market_ref_paths = [path for path in market_ref_paths if path.exists()]
    missing_market_ref_paths = [path for path in market_ref_paths if not path.exists()]
    if not existing_market_ref_paths:
        _write_state(
            state_path,
            {
                "schema_name": "resolution_feed_state",
                "schema_version": 1,
                "status": "missing_input",
                "last_refresh_at": _iso(now_dt),
                "decision_ledger_path": str(market_ref_paths[0]) if market_ref_paths else "",
                "decision_ledger_paths": [str(path) for path in existing_market_ref_paths],
                "market_ref_paths": [str(path) for path in market_ref_paths],
                "configured_decision_ledger_paths": [str(path) for path in market_ref_paths],
                "used_decision_ledger_paths": [],
                "missing_decision_ledger_paths": [str(path) for path in missing_market_ref_paths],
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
    # The cursor must advance by markets actually offered to the fetcher.
    # A smaller downstream max_markets cap must not silently skip queue IDs.
    fetch_limits = [
        limit for limit in (feed_cfg["max_incremental_markets"], feed_cfg["max_markets"])
        if limit is not None
    ]
    market_ref_path = _market_ref_path_for_refresh(
        existing_market_ref_paths,
        output_dir=output_dir,
        existing_latest_path=central_latest_resolution,
        mode=str(feed_cfg["mode"]),
        max_incremental_markets=min(fetch_limits) if fetch_limits else None,
        max_discovery_bytes=int(feed_cfg["max_discovery_bytes"]),
        max_index_bootstrap_bytes=int(feed_cfg["max_index_bootstrap_bytes"]),
        index_manifest_paths=[Path(path) for path in feed_cfg["market_ref_index_manifest_paths"]],
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
        "discovery": _load_state(output_dir / "resolution_market_refs.discovery.json").get("progress", {}),
        "last_refresh_at": _iso(now_dt),
        "decision_ledger_path": str(existing_market_ref_paths[0]) if existing_market_ref_paths else "",
        "decision_ledger_paths": [str(path) for path in existing_market_ref_paths],
        "market_ref_paths": [str(path) for path in market_ref_paths],
        "configured_decision_ledger_paths": [str(path) for path in market_ref_paths],
        "used_decision_ledger_paths": [str(path) for path in existing_market_ref_paths],
        "missing_decision_ledger_paths": [str(path) for path in missing_market_ref_paths],
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
    # A runtime config's explicit resolver block wins; a nested prediction-lab
    # block is only a base/default for collector-driven deployments.
    if isinstance(lab.get("resolution_feed"), Mapping):
        raw = {**dict(lab["resolution_feed"]), **raw}
    shadow = config.get("paper_shadow_lanes") if isinstance(config.get("paper_shadow_lanes"), Mapping) else {}
    decision_ledger_paths = _coerce_decision_ledger_paths(raw, shadow)
    configured_market_refs = raw.get("market_ref_paths") or raw.get("collector_market_paths") or []
    if not isinstance(configured_market_refs, (list, tuple)):
        configured_market_refs = [configured_market_refs]
    market_ref_paths = [str(path) for path in configured_market_refs if path not in (None, "")] or decision_ledger_paths
    output_dir = raw.get("output_dir") or DEFAULT_OUTPUT_DIR
    central_output_dir = raw.get("central_output_dir") or raw.get("canonical_output_dir") or output_dir
    index_cfg = lab.get("replay_index") if isinstance(lab.get("replay_index"), Mapping) else {}
    index_manifests = [
        str((Path(str(index_cfg["root_dir"])) if index_cfg.get("root_dir") else Path(path).parent / "replay_index")
            / "collector_replay_index.manifest.json")
        for path in market_ref_paths
    ]
    return {
        "enabled": bool(raw.get("enabled", False)),
        "decision_ledger_path": str(decision_ledger_paths[0]) if decision_ledger_paths else "",
        "decision_ledger_paths": [str(path) for path in decision_ledger_paths],
        "market_ref_paths": market_ref_paths,
        "decision_ledger_globs": list(_coerce_decision_ledger_globs(raw)),
        "output_dir": str(output_dir),
        "central_output_dir": str(central_output_dir),
        "mode": _resolution_feed_mode(raw.get("mode")),
        "interval_seconds": max(1, int(raw.get("interval_seconds", lab.get("resolve_interval_seconds", DEFAULT_INTERVAL_SECONDS)) or DEFAULT_INTERVAL_SECONDS)),
        "max_fetch_attempts": max(1, int(raw.get("max_fetch_attempts", 4) or 4)),
        "retry_delay_seconds": max(0.0, float(raw.get("retry_delay_seconds", 3.0) or 0.0)),
        "max_markets": _optional_int(raw.get("max_markets")),
        "max_incremental_markets": _optional_int(raw.get("max_incremental_markets")),
        "max_discovery_bytes": max(1, int(raw.get("max_discovery_bytes", DEFAULT_MAX_DISCOVERY_BYTES))),
        "max_index_bootstrap_bytes": max(1, int(raw.get("max_index_bootstrap_bytes", DEFAULT_MAX_INDEX_BOOTSTRAP_BYTES))),
        "market_ref_index_manifest_paths": list(dict.fromkeys(index_manifests)),
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


def _read_discovery_suffix(
    path: Path, *, offset: int, extent: int, budget: int, market_ids: set[str], index: bool = False,
) -> tuple[int, int]:
    """Read at most budget bytes, committing only newline-terminated records."""
    consumed = 0
    if offset >= extent or budget <= 0:
        return offset, consumed
    with path.open("rb") as handle:
        handle.seek(offset)
        while handle.tell() < extent and consumed < budget:
            payload = handle.readline(min(extent - handle.tell(), budget - consumed))
            consumed += len(payload)
            if not payload or not payload.endswith(b"\n"):
                if len(payload) == budget and offset == handle.tell() - len(payload):
                    raise ValueError(f"market discovery row exceeds byte budget: {path}")
                break
            offset = handle.tell()
            try:
                row = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                if index:
                    raise ValueError(f"invalid committed market discovery index row: {path}")
                continue
            if index and (not isinstance(row, Mapping) or row.get("schema_name") != "collector_replay_index"
                          or row.get("schema_version") != 1):
                raise ValueError(f"invalid committed market discovery index row: {path}")
            market_id = row_market_id(row) if isinstance(row, Mapping) else None
            if market_id:
                market_ids.add(market_id)
    return offset, consumed


def _discovery_index_seed(source: Path, stat: os.stat_result, manifests: list[Path]) -> dict[str, Any] | None:
    """Use only a collector checkpoint covering every raw row in its prefix.

    Compact rows discover IDs, never provide settlement authority. A replay
    index with skipped rows cannot replace raw discovery, even if its checksum
    is valid: those rows may still carry market IDs.
    """
    for manifest_path in manifests:
        manifest = _load_state(manifest_path)
        if (manifest.get("schema_name") != "collector_replay_index_manifest"
                or manifest.get("schema_version") != 2
                or manifest.get("index_schema_name") != "collector_replay_index"
                or manifest.get("index_schema_version") != 1
                or manifest.get("invalid_rows") != 0
                or manifest.get("source_path") != str(source)
                or manifest.get("archive_identity") != {"device": stat.st_dev, "inode": stat.st_ino}):
            continue
        extent = manifest.get("committed_index_bytes")
        prefix = manifest.get("indexed_source_bytes")
        index_path = Path(str(manifest.get("index_path") or ""))
        if (type(extent) is not int or extent < 0 or type(prefix) is not int or not 0 <= prefix <= stat.st_size
                or not index_path.is_file() or index_path.stat().st_size < extent):
            continue
        index_stat = index_path.stat()
        return {
            "path": str(index_path), "device": index_stat.st_dev, "inode": index_stat.st_ino,
            "extent": extent, "prefix_bytes": prefix, "offset": 0,
        }
    return None


def write_incremental_market_refs(
    input_paths: list[Path], *, output_dir: Path,
    max_read_bytes: int = DEFAULT_MAX_DISCOVERY_BYTES,
    max_index_bootstrap_bytes: int = DEFAULT_MAX_INDEX_BOOTSTRAP_BYTES,
    index_manifest_paths: list[Path] | None = None,
) -> Path:
    """Persist market discovery separately from bounded unresolved polling.

    The atomic document owns both IDs and complete-line source offsets, so a
    failed publication cannot advance past an ID that was not saved. Historical
    IDs remain eligible for unresolved retries when a source rotates/disappears.
    """
    checkpoint_path = output_dir / "resolution_market_refs.discovery.json"
    refs_path = output_dir / "resolution_market_refs.jsonl"
    with locked_file(output_dir / "resolution_market_refs.discovery.lock", "a+"):
        checkpoint = _load_state(checkpoint_path)
        market_ids = set(checkpoint.get("market_ids", []))
        if not checkpoint:
            # Keep the existing compact queue available during bounded migration.
            market_ids.update(mid for row in _load_resolution_rows(refs_path) if (mid := row_market_id(row)))
        sources = checkpoint.get("sources", {})
        raw_budget, index_budget = max_read_bytes, max_index_bootstrap_bytes
        raw_read = index_read = remaining_raw = remaining_index = 0
        for input_path in dict.fromkeys(input_paths):
            source = input_path.resolve()
            stat = source.stat()
            identity = {"device": stat.st_dev, "inode": stat.st_ino}
            previous = sources.get(str(source), {})
            seed = previous.get("seed_index")
            coverage = max(int(previous.get("offset", 0)), seed["prefix_bytes"] if seed else 0)
            truncated = stat.st_size < coverage
            if previous.get("identity") != identity or truncated:
                previous = {"identity": identity, "offset": 0}
                # A same-inode truncation invalidates its old compact prefix.
                seed = None if truncated else _discovery_index_seed(source, stat, index_manifest_paths or [])
                if seed:
                    previous["seed_index"] = seed
            if seed:
                if seed["offset"] == seed["extent"]:
                    # Keep following the collector's compact append stream.
                    # Routine discovery should scale with index rows rather
                    # than the much larger raw payloads they describe.
                    current = _discovery_index_seed(source, stat, index_manifest_paths or [])
                    if (current and current["path"] == seed["path"]
                            and (current["device"], current["inode"]) == (seed["device"], seed["inode"])
                            and current["extent"] >= seed["extent"]
                            and current["prefix_bytes"] >= seed["prefix_bytes"]):
                        seed.update(extent=current["extent"], prefix_bytes=current["prefix_bytes"])
                    else:
                        # An unavailable/incomplete replacement index cannot
                        # cover new raw rows. Resume from saved raw coverage.
                        del previous["seed_index"]
                        seed = None
            if seed:
                index_path = Path(seed["path"])
                index_stat = index_path.stat() if index_path.is_file() else None
                if (index_stat is None
                        or (index_stat.st_dev, index_stat.st_ino) != (seed["device"], seed["inode"])
                        or index_stat.st_size < seed["extent"]):
                    del previous["seed_index"]
                    seed = None
            if seed:
                seed["offset"], read = _read_discovery_suffix(
                    index_path, offset=seed["offset"], extent=seed["extent"], budget=index_budget,
                    market_ids=market_ids, index=True,
                )
                index_budget -= read
                index_read += read
                remaining_index += seed["extent"] - seed["offset"]
                if seed["offset"] == seed["extent"]:
                    previous["offset"] = max(previous["offset"], seed["prefix_bytes"])
                else:
                    sources[str(source)] = previous
                    remaining_raw += stat.st_size - previous["offset"]
                    continue
            previous["offset"], read = _read_discovery_suffix(
                input_path, offset=previous["offset"], extent=stat.st_size, budget=raw_budget, market_ids=market_ids,
            )
            raw_budget -= read
            raw_read += read
            remaining_raw += stat.st_size - previous["offset"]
            sources[str(source)] = previous
        atomic_write_json(checkpoint_path, {
            "schema_name": "resolution_market_discovery", "schema_version": 1,
            "market_ids": sorted(market_ids), "sources": sources,
            "progress": {"raw_bytes_read": raw_read, "index_bytes_read": index_read,
                         "remaining_raw_bytes": remaining_raw, "remaining_index_bootstrap_bytes": remaining_index},
        })
        rewrite_jsonl(refs_path, ({"market_id": mid} for mid in sorted(market_ids)))
    return refs_path


def _market_ref_path_for_refresh(
    decision_ledger_paths: list[Path],
    *,
    output_dir: Path,
    existing_latest_path: Path,
    mode: str,
    max_incremental_markets: int | None,
    max_discovery_bytes: int = DEFAULT_MAX_DISCOVERY_BYTES,
    max_index_bootstrap_bytes: int = DEFAULT_MAX_INDEX_BOOTSTRAP_BYTES,
    index_manifest_paths: list[Path] | None = None,
) -> Path:
    if mode != "incremental_unresolved":
        return write_unique_market_refs(decision_ledger_paths, output_dir=output_dir)

    all_refs_path = write_incremental_market_refs(
        decision_ledger_paths, output_dir=output_dir, max_read_bytes=max_discovery_bytes,
        max_index_bootstrap_bytes=max_index_bootstrap_bytes, index_manifest_paths=index_manifest_paths,
    )
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

    pending_path = output_dir / "resolution_market_refs.pending.jsonl"
    if max_incremental_markets is not None:
        limit = max(0, int(max_incremental_markets))
        last_attempted = None
        if limit and pending_path.is_file():
            with pending_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        previous = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(previous, Mapping):
                        last_attempted = row_market_id(previous) or last_attempted
        if last_attempted and pending_market_ids:
            start = bisect_right(pending_market_ids, last_attempted) % len(pending_market_ids)
            pending_market_ids = pending_market_ids[start:] + pending_market_ids[:start]
        pending_market_ids = pending_market_ids[:limit]

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
